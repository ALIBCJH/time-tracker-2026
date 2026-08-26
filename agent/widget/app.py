"""The desktop widget: a ring, a status line, and a tray menu.

Deliberately small. The face shows the ring, the time, the goal and how much is
left, and nothing else — every extra number added to a timer someone glances at
forty times a day makes the glance slower. Anything more interesting belongs on
the dashboard or in the report.

All the decisions live in widget/state.py, which has no Qt in it and is tested.
This file draws what that object says.
"""
import sys
from datetime import datetime, timezone

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import (QAction, QColor, QFont, QIcon, QPainter, QPainterPath,
                         QPen, QPixmap)
from PyQt6.QtWidgets import (QApplication, QHBoxLayout, QInputDialog, QLabel,
                             QMenu, QPushButton, QSystemTrayIcon, QVBoxLayout,
                             QWidget)

from widget.state import format_hm

INK = QColor('#0b0b0b')
MUTED = QColor('#898781')
TRACK = QColor('#e8e7e2')
BLUE = QColor('#2a78d6')
GOLD = QColor('#eda100')
VIOLET = QColor('#7a5cd6')
GREY = QColor('#b6b4ae')

CARD_WIDTH = 288
RING = 164
DOCK_GAP = 8


class RingGauge(QWidget):
    """A 270° arc. Not a full circle: the gap gives the arc a beginning and an
    end, so how far round it has gone is readable at a glance instead of
    needing a starting point to be inferred."""

    START = 225 * 16          # Qt angles are in sixteenths of a degree
    SPAN = -270 * 16

    def __init__(self):
        super().__init__()
        self.setFixedSize(RING, RING)
        self.progress = 0.0
        self.overdrive = False
        self.paused = False
        self.offline = False
        self.centre = ''
        self.caption = ''

    def paintEvent(self, _):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        inset = 11
        box = QRectF(inset, inset, self.width() - 2 * inset, self.height() - 2 * inset)

        painter.setPen(QPen(TRACK, 13, cap=Qt.PenCapStyle.RoundCap))
        painter.drawArc(box, self.START, self.SPAN)

        if self.paused:
            colour = GREY
        elif self.offline:
            colour = MUTED
        elif self.overdrive:
            colour = GOLD
        else:
            colour = BLUE

        fraction = min(1.0, max(0.0, self.progress))
        if fraction > 0:
            painter.setPen(QPen(colour, 13, cap=Qt.PenCapStyle.RoundCap))
            painter.drawArc(box, self.START, int(self.SPAN * fraction))

        # Past the goal, a second lap is drawn inside the first rather than
        # letting the arc silently stop growing.
        if self.overdrive:
            extra = min(1.0, self.progress - 1.0)
            inner = box.adjusted(11, 11, -11, -11)
            painter.setPen(QPen(VIOLET, 5, cap=Qt.PenCapStyle.RoundCap))
            painter.drawArc(inner, self.START, int(self.SPAN * extra))

        painter.setPen(INK)
        painter.setFont(QFont(self.font().family(), 24, QFont.Weight.Bold))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, self.centre)

        painter.setPen(MUTED)
        painter.setFont(QFont(self.font().family(), 9))
        painter.drawText(QRectF(0, self.height() * 0.63, self.width(), 20),
                         Qt.AlignmentFlag.AlignHCenter, self.caption)
        painter.end()


class Widget(QWidget):
    """The floating card."""

    def __init__(self, state, on_sync=None):
        super().__init__(None, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.state = state
        self.on_sync = on_sync
        self._drag = None

        self.gauge = RingGauge()
        self.status = QLabel()
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.togo = QLabel()
        self.togo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.togo.setStyleSheet(f'color:{MUTED.name()};font-size:12px')

        self.start = QPushButton('Start session')
        self.start.clicked.connect(self.toggle_session)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(self.gauge, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.status)
        layout.addWidget(self.togo)
        layout.addWidget(self.start)
        self.setFixedWidth(CARD_WIDTH)

        self.setStyleSheet("""
            QLabel { font-size: 14px; font-weight: 600; color: #0b0b0b; }
            QPushButton { background:#2a78d6; color:#fff; border:0; padding:9px;
                          border-radius:9px; font-weight:600; }
            QPushButton:hover { background:#2569bb; }
        """)
        self.dock()

    # ── Position ─────────────────────────────────────────────────────────────

    def dock(self):
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.center().x() - CARD_WIDTH // 2, screen.top() + DOCK_GAP)
        self._docked = True

    def mousePressEvent(self, event):
        self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag:
            self.move(event.globalPosition().toPoint() - self._drag)
            self._docked = False

    def paintEvent(self, _):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 16, 16)
        painter.fillPath(path, QColor(255, 255, 255, 246))
        painter.setPen(QPen(QColor('#dedcd5'), 1))
        painter.drawPath(path)
        painter.end()

    # ── Behaviour ────────────────────────────────────────────────────────────

    def toggle_session(self):
        if self.state.tracking:
            self.state.stop_session()
        else:
            project, ok = QInputDialog.getText(self, 'Start session', 'Project:',
                                               text='Digital Transformation')
            if not ok or not project.strip():
                return
            self.state.start_session(project.strip())
        self.render_state()
        if self.on_sync:
            self.on_sync()

    def render_state(self):
        s = self.state
        self.gauge.progress = s.progress
        self.gauge.overdrive = s.overdrive
        self.gauge.paused = s.paused
        self.gauge.offline = s.offline
        self.gauge.centre = format_hm(s.done_seconds)
        self.gauge.caption = 'TODAY' if s.mode == 'today' else 'THIS WEEK'
        self.gauge.update()

        self.status.setText(s.status_line)
        if s.paused:
            self.togo.setText('Nothing is being recorded')
        elif s.offline:
            self.togo.setText('Offline — still recording locally')
        elif s.overdrive:
            self.togo.setText(f'{format_hm(s.done_seconds - s.goal_seconds)} past goal')
        else:
            self.togo.setText(f'{format_hm(s.remaining_seconds)} to go')

        self.start.setText('Stop session' if s.tracking else 'Start session')
        self.start.setEnabled(not s.paused)
        self.adjustSize()
        self.setFixedWidth(CARD_WIDTH)
        if getattr(self, '_docked', True):
            self.dock()

    def mouseDoubleClickEvent(self, _):
        self.state.toggle_mode()
        self.render_state()


def tray_icon(app, widget, state, controller):
    icon = QSystemTrayIcon(app)
    pixmap = QPixmap(22, 22)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(BLUE, 3))
    painter.drawArc(QRectF(3, 3, 16, 16), 225 * 16, -270 * 16)
    painter.end()
    icon.setIcon(QIcon(pixmap))

    menu = QMenu()

    def add(text, handler):
        action = QAction(text, menu)
        action.triggered.connect(handler)
        menu.addAction(action)
        return action

    add('Show / hide', lambda: widget.setVisible(not widget.isVisible()))
    add('Dock to top centre', widget.dock)
    menu.addSeparator()
    # The pause belongs where it can be reached in one click. Someone who has
    # to find a browser tab to stop being recorded does not really have a
    # control.
    add('Pause tracking…', controller.pause)
    add('Open dashboard', controller.open_dashboard)
    menu.addSeparator()
    add('Quit', app.quit)

    icon.setContextMenu(menu)
    icon.setToolTip('TimeTracker')
    icon.show()
    return icon
