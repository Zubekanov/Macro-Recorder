"""The bottom pane: Output Log and Variables tabs."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Iterable, Mapping

MAX_LOG_LINES = 5000


def variable_rows(variables: Mapping[str, Any]) -> list[tuple[str, str]]:
    """``(name, value)`` rows sorted by name, with strings quoted as expressions write them."""
    out = []
    for name in sorted(variables, key=str.lower):
        value = variables[name]
        text = "'%s'" % value if isinstance(value, str) else str(value)
        out.append((name, text))
    return out


class LogPanel(ttk.Notebook):
    """Output Log (read-only text, capped) and Variables (name and value)."""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self._log_frame = ttk.Frame(self)
        self._text = tk.Text(self._log_frame, height=6, wrap="none", state="disabled",
                             undo=False)
        scroll = ttk.Scrollbar(self._log_frame, orient="vertical", command=self._text.yview)
        self._text.configure(yscrollcommand=scroll.set)
        clear = ttk.Button(self._log_frame, text="Clear", command=self.clear)
        self._text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        clear.grid(row=1, column=0, columnspan=2, sticky="e")
        self._log_frame.rowconfigure(0, weight=1)
        self._log_frame.columnconfigure(0, weight=1)
        self.add(self._log_frame, text="Output Log")

        self._vars_frame = ttk.Frame(self)
        self._vars = ttk.Treeview(self._vars_frame, columns=("name", "value"), show="headings",
                                  height=6, selectmode="browse")
        self._vars.heading("name", text="Name")
        self._vars.heading("value", text="Value")
        self._vars.column("name", width=160, stretch=False)
        self._vars.column("value", width=400)
        vscroll = ttk.Scrollbar(self._vars_frame, orient="vertical", command=self._vars.yview)
        self._vars.configure(yscrollcommand=vscroll.set)
        self._vars.grid(row=0, column=0, sticky="nsew")
        vscroll.grid(row=0, column=1, sticky="ns")
        self._vars_frame.rowconfigure(0, weight=1)
        self._vars_frame.columnconfigure(0, weight=1)
        self.add(self._vars_frame, text="Variables")

    def append(self, lines: Iterable[str]) -> None:
        """Add lines at the end, keep the newest MAX_LOG_LINES and follow the end
        unless the user scrolled up."""
        text = "".join(line.rstrip("\n") + "\n" for line in lines)
        if not text:
            return
        at_end = self._text.yview()[1] >= 0.999
        self._text.configure(state="normal")
        self._text.insert("end", text)
        excess = int(self._text.index("end-1c").split(".")[0]) - 1 - MAX_LOG_LINES
        if excess > 0:
            self._text.delete("1.0", "%d.0" % (excess + 1))
        self._text.configure(state="disabled")
        if at_end:
            self._text.see("end")

    def clear(self) -> None:
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.configure(state="disabled")

    def text(self) -> str:
        """The whole log (for tests)."""
        return self._text.get("1.0", "end-1c")

    def show_variables(self, variables: Mapping[str, Any]) -> None:
        self._vars.delete(*self._vars.get_children())
        for name, value in variable_rows(variables):
            self._vars.insert("", "end", values=(name, value))

    def variables(self) -> list[tuple[str, str]]:
        """The rows of the Variables tab (for tests)."""
        out = []
        for item in self._vars.get_children():
            name, value = (str(v) for v in self._vars.item(item, "values"))
            out.append((name, value))
        return out
