"""The evening card: what the machine saw, waiting for a sentence from you.

The draft is shown, not pre-filled into the box. A pre-filled note gets
accepted unread, and an accepted-unread note is worse than no note — it looks
like a record of the day and is really a record of the summarizer.

The proportions are shown as bars because the useful question at the end of a
day is not "how many minutes was that" but "where did it actually go", and a
list of durations answers the first while burying the second.
"""
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                             QTextEdit, QVBoxLayout, QWidget)

INK = '#0b0b0b'
MUTED = '#898781'
LINE = '#e1e0d9'
BLUE = '#2a78d6'

PALETTE = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300']
OTHER = '#898781'

MAX_ROWS = 5


def format_hm(seconds):
    hours, minutes = divmod(max(0, int(seconds)) // 60, 60)
    return f'{hours}h {minutes:02d}m' if hours else f'{minutes}m'


class ProportionBar(QWidget):
    """One horizontal bar split by activity — the shape of a day in 8 pixels."""

    def __init__(self, activities):
        super().__init__()
        self.setFixedHeight(10)
        total = sum(a.get('seconds', 0) for a in activities) or 1
        self.slices = [(a.get('seconds', 0) / total,
                        PALETTE[i] if i < len(PALETTE) else OTHER)
                       for i, a in enumerate(activities)]

    def paintEvent(self, _):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        x, width = 0.0, self.width()
        for fraction, colour in self.slices:
            span = fraction * width
            painter.fillRect(int(x), 1, max(1, int(span)), 8, QColor(colour))
            x += span
        painter.end()


class PromptCard(QWidget):
    """Confirm, skip, or leave a day as it was."""

    def __init__(self, prompt, on_answer, parent=None):
        super().__init__(parent)
        self.prompt = prompt
        self.on_answer = on_answer

        activities = (prompt.get('activities') or [])[:MAX_ROWS]
        is_topup = prompt.get('kind') == 'topup'

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        heading = QLabel('What did you do on ' + prompt['date'] + '?'
                         if not is_topup else 'You did a bit more on ' + prompt['date'])
        heading.setStyleSheet(f'font-size:15px;font-weight:700;color:{INK}')
        heading.setWordWrap(True)
        layout.addWidget(heading)

        tracked = QLabel(format_hm(prompt.get('tracked_seconds', 0)) + ' tracked')
        tracked.setStyleSheet(f'font-size:12px;color:{MUTED}')
        layout.addWidget(tracked)

        if activities:
            layout.addWidget(ProportionBar(activities))
            for index, activity in enumerate(activities):
                row = QHBoxLayout()
                colour = PALETTE[index] if index < len(PALETTE) else OTHER
                dot = QLabel('●')
                dot.setStyleSheet(f'color:{colour};font-size:11px')
                name = QLabel(activity.get('label', '')[:44])
                name.setStyleSheet(f'font-size:12px;color:{INK}')
                span = QLabel(format_hm(activity.get('seconds', 0)))
                span.setStyleSheet(f'font-size:12px;color:{MUTED}')
                row.addWidget(dot)
                row.addWidget(name, 1)
                row.addWidget(span)
                layout.addLayout(row)

        hint = QLabel('In your own words — this is the only part that knows why.')
        hint.setStyleSheet(f'font-size:11px;color:{MUTED}')
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.note = QTextEdit()
        self.note.setFixedHeight(64)
        # Deliberately empty. Pre-filling with the draft gets it accepted
        # unread, and an accepted-unread note looks like a record of the day
        # while really being a record of the summarizer.
        self.note.setPlaceholderText(prompt.get('headline') or '')
        self.note.setStyleSheet(
            f'border:1px solid {LINE};border-radius:8px;padding:7px;font-size:13px')
        layout.addWidget(self.note)

        buttons = QHBoxLayout()
        save = QPushButton('Save')
        save.clicked.connect(self._confirm)
        save.setStyleSheet(f'background:{BLUE};color:#fff;border:0;padding:8px 14px;'
                           f'border-radius:8px;font-weight:600')
        buttons.addWidget(save)

        if is_topup:
            # A top-up must be dismissible without overwriting the note already
            # written — the card sends an empty box alongside "leave as is".
            leave = QPushButton('Leave as it was')
            leave.clicked.connect(lambda: self._answer('unchanged'))
            buttons.addWidget(leave)
        else:
            skip = QPushButton('Skip this day')
            skip.clicked.connect(lambda: self._answer('skipped'))
            buttons.addWidget(skip)

        for button in (buttons.itemAt(i).widget() for i in range(1, buttons.count())):
            button.setStyleSheet(f'background:transparent;color:{MUTED};'
                                 f'border:1px solid {LINE};padding:8px 14px;'
                                 f'border-radius:8px')
        layout.addLayout(buttons)

        self.error = QLabel()
        self.error.setStyleSheet('color:#b3261e;font-size:12px')
        self.error.setVisible(False)
        layout.addWidget(self.error)

        self.setFixedWidth(300)
        self.setStyleSheet('background:#ffffff')

    def _confirm(self):
        text = self.note.toPlainText().strip()
        if not text:
            # An empty confirm would record the day as answered while saying
            # nothing about it — worse than leaving it in the queue.
            self.error.setText('Write a line first, or skip the day.')
            self.error.setVisible(True)
            return
        self._answer('confirmed', text)

    def _answer(self, status, note=''):
        self.on_answer(self.prompt['date'], note, status)
        self.close()
