"""The interactive interface for install and remove.

It runs a planned transaction: pick a package where names tie, confirm a
fork/replacement, review the plan, then watch the native output stream into the
log. The plan itself is built by planner.py; this module only shows and runs it.
Textual is imported here, so the plain and JSON paths never load it.

Keys: Enter confirms, Esc cancels, q quits once the run has finished.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from . import planner, resolver
from .model import Candidate
from .planner import Draft, Plan, PlanError
from .transaction import arun


@dataclass
class Job:
    draft: Draft
    ctx: resolver.Context
    cfg: dict
    # Called once the run ends: (plan, exit code, every output line).
    on_done: Callable[[Plan, int, list[str]], None] | None = None


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

        def __init__(self, job: Job):
            super().__init__()
            self.job = job
            self.result: int | None = None  # set when the flow has finished
            self.output: list[str] = []  # every line shown in the log view, kept for the transaction log

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Static("", id="status")
            yield RichLog(id="log", wrap=True, highlight=False, markup=False)
            yield Footer()

        def on_mount(self) -> None:
            names = " ".join(r.term for r in self.job.draft.resolutions)
            self.sub_title = f"{self.job.draft.intent.op} {names}"
            self._set_status("resolving")
            for res in self.job.draft.resolutions:
                for note in res.notes:
                    self.log_line(note)
                for hint in res.hints:
                    self.log_line(f"also in {hint.source} as {hint.ref} (off; use --{hint.source})")
            self.run_worker(self._flow(), exclusive=True)

        def log_line(self, text: str) -> None:
            self.output.append(text)
            self.query_one(RichLog).write(text)

        def _set_status(self, text: str) -> None:
            self.query_one("#status", Static).update(text)

        def _finish(self, rc: int, message: str, plan: Plan | None = None) -> None:
            self.result = rc
            self.log_line(message)
            self._set_status(f"{message}   [b]press q to close[/b]")
            if self.job.on_done is not None and plan is not None:
                self.job.on_done(plan, rc, self.output)

        async def _flow(self) -> None:
            job = self.job
            d, ctx, cfg = job.draft, job.ctx, job.cfg
            if not d.ok:
                return self._finish(d.failures[0][0], d.failures[0][1])

            chosen: list[Candidate] = []
            for i, res in enumerate(d.resolutions):
                if res.replacement and i in d.forks():
                    question = (f"'{res.replacement}' is a different project replacing '{res.term}'. "
                                "Use it instead?")
                    if not await self.push_screen_wait(ConfirmScreen(question)):
                        return self._finish(4, "refused: replacement not confirmed")
                if res.tie:
                    options = [(c, planner.preview(d, c, ctx)) for c in res.ranked]
                    picked = await self.push_screen_wait(PickScreen(res.term, [c for c, _ in options],
                                                                    [cmd for _, cmd in options]))
                    if picked is None:
                        return self._finish(1, f"cancelled: '{res.term}' not chosen")
                    chosen.append(picked)
                else:
                    chosen.append(res.best)

            try:
                plan = planner.finalize(d, chosen, ctx, cfg, assume_yes=True)
            except PlanError as exc:
                for line in exc.message.splitlines():
                    self.log_line(line)
                return self._finish(exc.code, "not run: the plan is invalid; nothing was changed")

            sudo = cfg["general"]["sudo"]
            if not await self.push_screen_wait(PlanScreen(plan.render(sudo) + ["", "[b]Enter[/b] run   [b]Esc[/b] cancel"])):
                return self._finish(1, "cancelled before running anything")

            self._set_status("running")
            rc = await arun(plan.steps, cfg, plan.label, self.log_line, self._suspend_run)
            if rc == 0:
                self._finish(0, "done", plan)
            else:
                self._finish(rc, f"failed with status {rc}", plan)

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


def run_job(job: Job) -> int:
    """Run the interactive flow and return the transaction's exit code."""
    rc = _app_classes()(job).run()
    return 1 if rc is None else rc


def make_app(job: Job):
    """Used by the tests to drive the app headlessly."""
    return _app_classes()(job)
