"""A search box over a cube's variables and axis values, with a popup of matches.

One widget for the sidebar and the load dialog: the host hands it the entries (see
:mod:`datacube_viewer.core.search`) and acts on :attr:`FuzzySearchBox.picked`. The
box ranks on every keystroke and shows the best matches in a completer popup; Enter
takes the best one, so a precise query is a single keystroke away from its slice.

``QCompleter`` filters by prefix or substring itself, which is not fuzzy, so it is
run unfiltered over a model this widget refills with its own ranking.
"""

from __future__ import annotations

from collections.abc import Sequence

from qgis.gui import QgsFilterLineEdit
from qgis.PyQt.QtCore import QModelIndex, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import QCompleter, QWidget

from ..core.search import DEFAULT_LIMIT, SearchEntry, rank

_ENTRY_ROLE = Qt.ItemDataRole.UserRole + 1


class FuzzySearchBox(QgsFilterLineEdit):
    """Type to search; pick from the popup, or press Enter for the best match."""

    #: A :class:`~datacube_viewer.core.search.SearchEntry` was chosen.
    picked = pyqtSignal(object)

    def __init__(self, placeholder: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setShowSearchIcon(True)
        self._entries: list[SearchEntry] = []
        self._results: list[SearchEntry] = []

        self._model = QStandardItemModel(self)
        self._completer = QCompleter(self._model, self)
        self._completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self._completer.setMaxVisibleItems(12)
        self._completer.setWidget(self)
        self._completer.activated[QModelIndex].connect(self._on_activated)

        self.textEdited.connect(self._on_edited)
        self.returnPressed.connect(self.pick_best)

    def set_entries(self, entries: Sequence[SearchEntry]) -> None:
        """What can be found. Clears the query, which belonged to the old entries."""
        self._entries = list(entries)
        self.clear()
        self._fill([])
        self.setEnabled(bool(self._entries))

    def results(self) -> list[SearchEntry]:
        """The current matches, best first."""
        return list(self._results)

    def search(self, query: str) -> list[SearchEntry]:
        """Rank the entries for ``query`` and offer them in the popup."""
        self._results = rank(query, self._entries, lambda entry: entry.text, DEFAULT_LIMIT)
        self._fill(self._results)
        return self.results()

    def pick(self, entry: SearchEntry) -> None:
        self._completer.popup().hide()
        self.picked.emit(entry)
        # Later, not now: the completer writes the chosen row's text into the box
        # after it emits, and the box should end up empty, ready for the next one.
        QTimer.singleShot(0, self.clear)

    def pick_best(self) -> None:
        """Enter: take the best match of what is typed, if anything matches."""
        results = self.search(self.text()) if self.text().strip() else []
        if results:
            self.pick(results[0])

    def _on_edited(self, text: str) -> None:
        if not text.strip():
            self._results = []
            self._completer.popup().hide()
            return
        if self.search(text):
            self._completer.complete()
        else:
            self._completer.popup().hide()

    def _fill(self, entries: Sequence[SearchEntry]) -> None:
        self._model.clear()
        for entry in entries:
            item = QStandardItem(entry.label)
            item.setData(entry, _ENTRY_ROLE)
            item.setEditable(False)
            self._model.appendRow(item)

    def _on_activated(self, index: QModelIndex) -> None:
        entry = index.data(_ENTRY_ROLE)
        if entry is not None:
            self.pick(entry)
