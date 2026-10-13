"""The interactive interface for install and remove.

One app runs the whole transaction: pick a package where names tie, confirm a
fork/replacement, review the plan, then watch the native output stream into the
log. Textual is imported here, so the plain and JSON paths never load it.

Keys: Enter confirms, Esc cancels, q quits once the run has finished.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from .backends import Step
from .model import Candidate
from .resolver import Resolution
from .transaction import _as_root, arun, snapper_enabled


@dataclass
class Plan:
    title: str
    cfg: dict
    label: str
    resolutions: list[Resolution]
    accept: list[bool]  # --accept-replacement per term
    build: Callable[[list[Candidate]], list[Step]]  # chosen candidates -> native steps
    notices: list[str]  # "Understood as" lines and similar, shown on the plan
    # Called once the run ends: (chosen, steps, exit code, every output line).
    on_done: Callable[[list[Candidate], list[Step], int, list[str]], None] | None = None


def _app_classes():
    from textual.app import App, ComposeResult
    from textual.containers import Vertical
    from textual.screen import ModalScreen
    from textual.widgets import DataTable, Footer, Header, RichLog, Static

    class PickScreen(ModalScreen[Candidate | None]):
        BINDINGS = [("escape", "cancel", "Cancel")]
        DEFAULT_CSS = """
        PickScreen { align: center middle; }
        #pick { width: 90%; height: auto; max-height: 90%; border: round $accent; padding: 1 2; background: $surface; }
        #preview { margin-top: 1; color: $text-muted; }
        """

        def __init__(self, term: str, options: list[Candidate], commands: list[str]):
            super().__init__()
            self.term, self.options, self.commands = term, options, commands

        def compose(self) -> ComposeResult:
            with Vertical(id="pick"):
                yield Static(f"[b]Several packages match '{self.term}'[/b]. Pick one (Enter), Esc cancels.")
                yield DataTable(cursor_type="row", id="options")
                yield Static("", id="preview")

        def on_mount(self) -> None:
            table = self.query_one(DataTable)
            table.add_columns("#", "Source", "Name", "Version", "Description")
            for i, cand in enumerate(self.options, 1):
                table.add_row(str(i), cand.source, cand.ref, cand.version, cand.description)
            self._preview(0)
            table.focus()

        def _preview(self, row: int) -> None:
            self.query_one("#preview", Static).update(f"Will run:  $ {self.commands[row]}")

        def on_data_table_row_highlighted(self, event) -> None:
            self._preview(event.cursor_row)

        def on_data_table_row_selected(self, event) -> None:
            self.dismiss(self.options[event.cursor_row])

        def action_cancel(self) -> None:
            self.dismiss(None)

    class ConfirmScreen(ModalScreen[bool]):
        BINDINGS = [("y", "yes", "Yes"), ("n", "no", "No"), ("escape", "no", "No")]
        DEFAULT_CSS = """
        ConfirmScreen { align: center middle; }
        #ask { width: 80%; height: auto; border: round $warning; padding: 1 2; background: $surface; }
        """

        def __init__(self, message: str):
            super().__init__()
            self.message = message

        def compose(self) -> ComposeResult:
            with Vertical(id="ask"):
                yield Static(self.message + "\n\n[b]y[/b] yes   [b]n[/b] no")

        def action_yes(self) -> None:
            self.dismiss(True)

        def action_no(self) -> None:
            self.dismiss(False)

    class PlanScreen(ModalScreen[bool]):
        BINDINGS = [("enter", "yes", "Run"), ("y", "yes", "Run"), ("escape", "no", "Cancel"), ("n", "no", "Cancel")]
        DEFAULT_CSS = """
        PlanScreen { align: center middle; }
        #plan { width: 90%; height: auto; max-height: 90%; border: round $success; padding: 1 2; background: $surface; }
        """

        def __init__(self, lines: list[str]):
            super().__init__()
            self.lines = lines

        def compose(self) -> ComposeResult:
            with Vertical(id="plan"):
                yield Static("\n".join(self.lines) + "\n\n[b]Enter[/b] run   [b]Esc[/b] cancel")

        def action_yes(self) -> None:
            self.dismiss(True)

        def action_no(self) -> None:
            self.dismiss(False)

    class UniApp(App[int]):
        TITLE = "uni"
        CSS = "#status { padding: 0 1; background: $panel; }"
        BINDINGS = [("q", "quit_app", "Quit"), ("ctrl+c", "abort", "Abort")]

        def __init__(self, plan: Plan):
            super().__init__()
            self.plan = plan
            self.result: int | None = None  # set when the flow has finished
            self.output: list[str] = []  # every line shown in the log view, kept for the transaction log

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Static("", id="status")
            yield RichLog(id="log", wrap=True, highlight=False, markup=False)
            yield Footer()

        def on_mount(self) -> None:
            self.sub_title = self.plan.title
            self._set_status("resolving")
            for note in self.plan.notices:
                self.log_line(note)
            self.run_worker(self._flow(), exclusive=True)

        def log_line(self, text: str) -> None:
            self.output.append(text)
            self.query_one(RichLog).write(text)

        def _set_status(self, text: str) -> None:
            self.query_one("#status", Static).update(text)

        def _finish(self, rc: int, message: str, chosen=None, steps=None) -> None:
            self.result = rc
            self.log_line(message)
            self._set_status(f"{message}   [b]press q to close[/b]")
            if self.plan.on_done is not None and steps is not None:
                self.plan.on_done(chosen or [], steps, rc, self.output)

        async def _flow(self) -> None:
            plan = self.plan
            chosen: list[Candidate] = []
            for term, res, accept in zip((r.term for r in plan.resolutions), plan.resolutions, plan.accept):
                for hint in res.hints:
                    self.log_line(f"also in {hint.source} as {hint.ref} (off; use --{hint.source})")
                if res.replacement and not accept:
                    question = f"'{res.replacement}' is a different project replacing '{term}'. Use it instead?"
                    if not await self.push_screen_wait(ConfirmScreen(question)):
                        return self._finish(4, "refused: replacement not confirmed")
                if res.tie:
                    commands = [" ; ".join(" ".join(s.argv) for s in plan.build([c])) for c in res.ranked]
                    picked = await self.push_screen_wait(PickScreen(term, res.ranked, commands))
                    if picked is None:
                        return self._finish(1, f"cancelled: '{term}' not chosen")
                    chosen.append(picked)
                else:
                    chosen.append(res.best)

            steps = plan.build(chosen)
            lines = ["[b]Plan[/b]"]
            lines += [f"  {c.source:<8} {c.ref} {c.version}".rstrip() for c in chosen]
            sudo = plan.cfg["general"]["sudo"]
            lines += ["", "[b]Commands[/b]"]
            if snapper_enabled(plan.cfg):
                lines.append("  snapshot before and after (snapper)")
            lines += [f"  $ {' '.join(_as_root(s.argv, s.root, sudo))}" for s in steps]
            lines += ["", *plan.notices]
            if not await self.push_screen_wait(PlanScreen(lines)):
                return self._finish(1, "cancelled before running anything")

            self._set_status("running")
            rc = await arun(steps, plan.cfg, plan.label, self.log_line, self._suspend_run)
            if rc == 0:
                self._finish(0, "done", chosen, steps)
            else:
                self._finish(rc, f"failed with status {rc}", chosen, steps)

        def _suspend_run(self, argv: list[str]) -> int:
            with self.suspend():
                return subprocess.run(argv, check=False).returncode

        def action_quit_app(self) -> None:
            if self.result is None:
                self.bell()  # refuse to quit mid-transaction
                return
            self.exit(self.result)

        def action_abort(self) -> None:
            if self.result is None:
                self.workers.cancel_all()
                self._finish(130, "aborted")
            else:
                self.exit(self.result)

    return UniApp


def run_plan(plan: Plan) -> int:
    """Run the interactive flow and return the transaction's exit code."""
    app_cls = _app_classes()
    app = app_cls(plan)
    rc = app.run()
    return 1 if rc is None else rc


def make_app(plan: Plan):
    """Used by the tests to drive the app headlessly."""
    return _app_classes()(plan)
