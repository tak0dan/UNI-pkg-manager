"""Interactive picker, shown only on real ties between equally ranked candidates.

Textual is imported lazily so the plain and JSON paths never need it.
"""

from __future__ import annotations

from .model import Candidate


def _app_class():
    from textual.app import App
    from textual.widgets import DataTable, Footer, Header, Static

    class PickApp(App[Candidate | None]):
        TITLE = "uni"
        BINDINGS = [("escape", "cancel", "Cancel"), ("q", "cancel", "Quit")]
        CSS = "#preview { padding: 1 2; border: round $accent; height: auto; }"

        def __init__(self, term: str, options: list[tuple[Candidate, str]]):
            super().__init__()
            self.term = term
            self.options = options

        def compose(self):
            yield Header()
            yield DataTable(cursor_type="row", id="results")
            yield Static("", id="preview")
            yield Footer()

        def on_mount(self) -> None:
            self.sub_title = f"choose a package for '{self.term}'"
            table = self.query_one(DataTable)
            table.add_columns("#", "Source", "Name", "Version", "Description")
            for i, (cand, _) in enumerate(self.options, 1):
                table.add_row(str(i), cand.source, cand.ref, cand.version, cand.description)
            self._show(0)
            table.focus()

        def _show(self, row: int) -> None:
            _, command = self.options[row]
            self.query_one("#preview", Static).update(f"Will run:\n$ {command}")

        def on_data_table_row_highlighted(self, event) -> None:
            self._show(event.cursor_row)

        def on_data_table_row_selected(self, event) -> None:
            self.exit(self.options[event.cursor_row][0])

        def action_cancel(self) -> None:
            self.exit(None)

    return PickApp


def pick(term: str, options: list[tuple[Candidate, str]]) -> Candidate | None:
    """options: (candidate, the native command it would run). First row is pre-selected."""
    return _app_class()(term, options).run()
